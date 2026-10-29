// Real DOM regression: select a paper without replacing the current conversation.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');

async function main() {
  const html = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8')
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, '');
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (url.pathname.startsWith('/static/css/')) {
      res.setHeader('Content-Type', 'text/css');
      res.end(fs.readFileSync(path.join(root, url.pathname)));
    } else {
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end(html);
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({ channel: 'msedge', headless: true })
      .catch(() => chromium.launch({ headless: true }));
    const page = await browser.newPage({ locale: 'fr-FR' });
    const errors = [];
    const requests = [];
    let failLibrary = false;
    let secondPageRead = false;
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/**', route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/library') {
        assert.equal(url.searchParams.get('indexed_only'), 'true');
        if (failLibrary) return route.fulfill({ status: 503, json: {} });
        const second = Number(url.searchParams.get('offset')) > 0;
        secondPageRead ||= second;
        return route.fulfill({ json: second ? {
          rows: [['Étude des matériaux', 'Martin', '2025'], ['Étude des matériaux', 'Dupont', '2024']],
          paths: ['papers/one.pdf', 'papers/two.pdf'], metadata: [{}, {}],
          pagination: { has_more: false },
        } : {
          rows: [['Other paper'], ['Étude exclue']], paths: ['other.pdf', 'excluded.pdf'],
          metadata: [{}, { rag_excluded: true }], pagination: { has_more: true },
        } });
      }
      if (url.pathname === '/api/chat') {
        requests.push(route.request().postDataJSON());
        return route.fulfill({ contentType: 'text/event-stream', body: 'data: {"type":"done"}\n\n' });
      }
      return route.fulfill({ json: { ok: true } });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    for (const file of ['static/js/event-bindings.js', 'static/js/i18n.js', 'static/js/app.js',
      'static/js/app/transport.js', 'static/js/app/chat.js', 'static/js/static-event-bindings.js']) {
      await page.addScriptTag({ path: path.join(root, file) });
    }
    await page.evaluate(() => {
      BibliobrainI18n.setLanguage('fr', { sync: false });
      document.getElementById('startup-boot-splash')?.remove();
      state.activeChatId = 'existing-chat';
      state.activeChatTitle = 'Conversation existante';
      document.getElementById('messages').innerHTML = '<p id="old-message">Message conservé</p>';
      document.getElementById('msg-input').value = 'Question en préparation';
      // Other chat dependencies are outside this UI regression's scope.
      window.syncOllamaBaseUrl = async () => {};
      window.requireSelectedModel = () => 'test-model';
      window.parseFilterInput = () => [];
      window.updateChatVisualForceUi = () => {};
      window.isDebugMetricsEnabled = () => false;
      window.isThinkingEnabled = () => false;
      window.isForceThinkingSmallModelsEnabled = () => false;
      window.getChatDepth = () => 'auto';
      window._appendUserBubble = () => {};
      window._appendAssistantPlaceholder = () => document.createElement('div');
      window.loadChatHistory = async () => {};
      window.refreshActiveChatIfStillOpen = async () => {};
      window.isVisualUpdateVisible = () => false;
    });
    const search = page.locator('#chat-article-search');
    const results = page.locator('.chat-article-search-result');
    await page.locator('#chat-options-btn').click();
    await search.fill('ETUDE materiaux');
    await results.first().waitFor();
    assert.equal(await results.count(), 2);
    assert.ok(secondPageRead, 'search covers paginated papers');
    assert.match(await results.nth(1).innerText(), /Dupont · 2024/);
    assert.equal(await results.nth(0).locator('strong mark').first().innerText(), 'Étude');
    // Authors and years are searched too, mixed with title words.
    await search.fill('dupont materiaux');
    await page.waitForFunction(() => document.querySelectorAll('.chat-article-search-result').length === 1);
    await search.fill('2025');
    await page.waitForFunction(() => document.querySelectorAll('.chat-article-search-result').length === 1);
    assert.match(await results.first().innerText(), /Martin · 2025/);
    await search.fill('ETUDE materiaux');
    await page.waitForFunction(() => document.querySelectorAll('.chat-article-search-result').length === 2);
    await results.nth(1).click();
    assert.equal(await page.locator('#chat-article-scope').isVisible(), true);
    assert.equal(await page.locator('#chat-options-menu').isVisible(), false);
    assert.equal(await page.locator('#msg-input').inputValue(), 'Question en préparation');
    assert.equal(await page.locator('#old-message').innerText(), 'Message conservé');
    assert.deepEqual(await page.evaluate(() => [state.activeChatId, state.chatArticleScope.path,
      state.chatArticleScope.once]), ['existing-chat', 'papers/two.pdf', false]);
    assert.match(await page.locator('#chat-options-count').innerText(), /1/);
    await page.evaluate(() => sendMessage());
    assert.equal(requests[0].chat_id, 'existing-chat');
    assert.equal(requests[0].article_path, 'papers/two.pdf');
    assert.equal(requests[0].article_scope_once, false);
    await page.evaluate(meta => { clearChatArticleScope(); restoreChatArticleScope([{ meta }]); }, requests[0]);
    assert.equal(await page.evaluate(() => state.chatArticleScope.path), 'papers/two.pdf');
    await page.locator('#chat-article-scope button').click();
    assert.equal(await page.locator('#chat-article-scope').isVisible(), false);
    assert.equal(await page.locator('#chat-options-count').isVisible(), false);
    await page.evaluate(() => sendMessage({ messageOverride: 'Sans filtre' }));
    assert.equal(requests[1].article_path, '');
    assert.equal(requests[1].chat_id, 'existing-chat');
    await page.locator('#chat-options-btn').click();
    await search.fill('exclue');
    await page.waitForFunction(() => document.getElementById('chat-article-search-status').textContent.includes('Aucun article'));
    assert.equal(await results.count(), 0);
    await search.fill('');
    assert.equal(await page.locator('#chat-article-search-results').isVisible(), false);
    await search.press('Escape');
    assert.equal(await page.locator('#chat-options-menu').isVisible(), false);
    failLibrary = true;
    await page.locator('#chat-options-btn').click();
    await page.waitForFunction(() => document.getElementById('chat-article-search-status').textContent.includes('Chargement impossible'));
    assert.equal(await results.count(), 0);
    await search.press('Escape');
    failLibrary = false;
    await page.locator('#chat-options-btn').click();
    await search.fill('matériaux');
    await results.first().waitFor();
    assert.equal(await results.count(), 2);
    await page.evaluate(() => BibliobrainI18n.setLanguage('en', { sync: false }));
    assert.match(await page.locator('#chat-article-search-status').innerText(), /Select a paper/);
    assert.equal(await results.first().locator('strong').innerText(), 'Étude des matériaux');
    await page.evaluate(() => BibliobrainI18n.setLanguage('fr', { sync: false }));
    await page.setViewportSize({ width: 390, height: 844 });
    await search.scrollIntoViewIfNeeded();
    const box = await search.boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= 390, 'search fits a phone viewport');
    await results.first().focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.evaluate(() => state.chatArticleScope.path), 'papers/one.pdf');
    assert.equal(await page.evaluate(() => state.activeChatId), 'existing-chat');
    assert.deepEqual(errors, []);
    console.log('PASS chat article filter: pagination, accents, scope, existing chat, reset, errors, mobile');
  } finally {
    await browser?.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
