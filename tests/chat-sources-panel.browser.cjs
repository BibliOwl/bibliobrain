// Run with Node and Playwright already available in NODE_PATH. No live backend is used.
// Covers how the chat sources panel closes: close button, Escape, backdrop and
// outside clicks on narrow windows, and where focus goes afterwards.
// Optional argument: a path where a screenshot of the narrow panel is written.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const screenshotPath = process.argv[2] || '';

// The panel code lives in chat.js next to the whole chat UI: load only its
// open/close functions and stub the rendering that needs a real answer.
function panelSource() {
  const source = fs.readFileSync(path.join(root, 'static/js/app/chat.js'), 'utf8');
  const start = source.indexOf('const chatSourcesPanel = {');
  const end = source.indexOf('// Keeps the panel in step with the answer it shows');
  assert.ok(start >= 0 && end > start, 'chat.js no longer matches the panel extraction markers');
  return source.slice(start, end);
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
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, locale: 'fr-FR' });
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));

    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(() => {
      document.getElementById('startup-boot-splash')?.remove();
      const messages = document.getElementById('messages');
      messages.innerHTML = `
        <div class="bubble" id="bubble-a">
          <a href="#" id="cite-a" data-source-label="S1">[S1]</a>
          <button type="button" id="stopper">stops bubbling</button>
        </div>`;
      document.getElementById('stopper').addEventListener('click', event => event.stopPropagation());
      document.getElementById('bubble-a')._chatSources = [{ ref_label: 'S1', title: 'Aubert' }];
    });
    await page.evaluate(() => {
      window.milestones = [];
      window.addEventListener('bibliobrain:milestone', event => window.milestones.push(event.detail.name));
      window._markActiveChatCitation = () => {};
      window.renderChatSourcesPanel = () => {};
      window.sourceRefLabel = (source, index) => source.ref_label || `S${index}`;
      window.normalizeAssistantCitationLabel = label => String(label || '').trim().toUpperCase();
      window.collectSourceLabelAliases = (source, index) => new Set([window.sourceRefLabel(source, index)]);
      window._ti = key => key;
    });
    // A classic script keeps the extracted declarations global, like in the app.
    await page.addScriptTag({ content: panelSource() });
    await page.evaluate(() => {
      document.getElementById('messages').addEventListener('click', event => {
        const trigger = event.target.closest('a[data-source-label]');
        if (!trigger) return;
        event.preventDefault();
        openChatSourcesPanel(trigger.closest('.bubble'), { label: trigger.dataset.sourceLabel, trigger });
      });
    });

    const open = (options = '{}') => page.evaluate(`openChatSourcesPanel(document.getElementById('bubble-a'), ${options})`);
    const state = () => page.evaluate(() => {
      const panel = document.getElementById('chat-sources-panel');
      return {
        exists: Boolean(panel),
        hidden: panel ? panel.hidden : null,
        layoutClass: document.getElementById('chat-layout').classList.contains('has-sources-panel'),
        focus: document.activeElement?.id || document.activeElement?.className || '',
      };
    });
    const backdropDisplay = () => page.evaluate(() => {
      const backdrop = document.getElementById('chat-sources-backdrop');
      return backdrop ? getComputedStyle(backdrop).display : 'missing';
    });
    const clickAt = (x, y) => page.mouse.click(x, y);

    // Nothing is created before the first open.
    assert.equal((await state()).exists, false);
    assert.equal(await backdropDisplay(), 'missing');

    // Opening creates the panel and its backdrop once, and moves focus into the panel.
    await open('{ label: "S1", trigger: document.getElementById("cite-a") }');
    let now = await state();
    assert.equal(now.hidden, false);
    assert.equal(now.layoutClass, true);
    assert.equal(now.focus, 'chat-sources-title');
    assert.equal(await page.locator('#chat-sources-backdrop').count(), 1);
    assert.deepEqual(await page.evaluate(() => window.milestones), ['source-opened']);
    await open();
    assert.equal(await page.locator('#chat-sources-backdrop').count(), 1, 'Reopening must not add a second backdrop');
    assert.equal(await page.locator('#chat-sources-panel').count(), 1);

    // Wide window: the panel sits beside the conversation, no backdrop, outside clicks keep it open.
    assert.equal(await backdropDisplay(), 'none');
    await page.click('#cite-a');
    assert.equal((await state()).hidden, false, 'A click outside must not close the side panel on a wide window');

    // The close button closes and gives focus back to the citation that opened it.
    await open('{ label: "S1", trigger: document.getElementById("cite-a") }');
    await page.click('#chat-sources-panel .csp-close');
    now = await state();
    assert.equal(now.hidden, true);
    assert.equal(now.layoutClass, false);
    assert.equal(now.focus, 'cite-a');

    // Escape inside the panel closes it too.
    await open('{ label: "S1", trigger: document.getElementById("cite-a") }');
    await page.keyboard.press('Escape');
    now = await state();
    assert.equal(now.hidden, true);
    assert.equal(now.focus, 'cite-a');

    // Closing with restoreFocus:false leaves focus alone (new chat, removed answer).
    await open('{ label: "S1", trigger: document.getElementById("cite-a") }');
    await page.evaluate(() => closeChatSourcesPanel({ restoreFocus: false }));
    now = await state();
    assert.equal(now.hidden, true);
    assert.notEqual(now.focus, 'cite-a');

    // Closing twice is harmless.
    await page.evaluate(() => closeChatSourcesPanel());

    // A trigger replaced by a re-render falls back to the citation with the same label.
    await open('{ label: "S1", trigger: document.getElementById("cite-a") }');
    await page.evaluate(() => {
      const old = document.getElementById('cite-a');
      const fresh = old.cloneNode(true);
      fresh.id = 'cite-a-new';
      old.replaceWith(fresh);
    });
    await page.click('#chat-sources-panel .csp-close');
    assert.equal((await state()).focus, 'cite-a-new');

    // Narrow window: the panel overlays the conversation and the backdrop is on.
    await page.setViewportSize({ width: 1000, height: 800 });
    await page.click('#cite-a-new');
    assert.equal(await backdropDisplay(), 'block');
    // The panel slides in: measure it once the entrance animation is done.
    await page.evaluate(() => Promise.all(document.getAnimations().map(animation => animation.finished.catch(() => {}))));
    const geometry = await page.evaluate(() => {
      const panel = document.getElementById('chat-sources-panel').getBoundingClientRect();
      const layout = document.getElementById('chat-layout').getBoundingClientRect();
      return { panelLeft: panel.left, panelWidth: panel.width, layoutLeft: layout.left, layoutRight: layout.right };
    });
    assert.ok(geometry.panelWidth <= 420, 'The panel is capped at 420px');
    assert.equal(geometry.panelLeft + geometry.panelWidth, geometry.layoutRight, 'The panel hugs the right edge');
    if (screenshotPath) await page.screenshot({ path: screenshotPath });

    // The click that opened the panel does not close it (capture listener is only added after it).
    assert.equal((await state()).hidden, false);

    // A click on the conversation closes it; the click target is not the backdrop, so focus is not restored.
    await clickAt(geometry.layoutLeft + 20, 300);
    now = await state();
    assert.equal(now.hidden, true, 'Clicking outside closes the overlay panel');
    assert.equal(now.layoutClass, false);

    // A control that stops bubbling still closes the panel (capture phase).
    await open('{ label: "S1", trigger: document.getElementById("cite-a-new") }');
    // The backdrop covers the control, so dispatch the click on it directly.
    await page.evaluate(() => document.getElementById('stopper').click());
    assert.equal((await state()).hidden, true, 'Capture phase must catch clicks whose bubbling is stopped');

    // Clicks inside the panel keep it open.
    await open('{ label: "S1", trigger: document.getElementById("cite-a-new") }');
    await page.click('#chat-sources-panel .csp-head', { position: { x: 8, y: 8 } });
    assert.equal((await state()).hidden, false, 'Clicking inside the panel must not close it');

    // Clicking the visible strip of the backdrop closes it and restores focus to the citation.
    const strip = await page.evaluate(() => {
      const backdrop = document.getElementById('chat-sources-backdrop').getBoundingClientRect();
      const panel = document.getElementById('chat-sources-panel').getBoundingClientRect();
      return { x: (backdrop.left + panel.left) / 2, y: backdrop.top + 200 };
    });
    assert.equal(
      await page.evaluate(({ x, y }) => document.elementFromPoint(x, y).id, strip),
      'chat-sources-backdrop',
      'The backdrop must cover the conversation left of the panel',
    );
    await clickAt(strip.x, strip.y);
    now = await state();
    assert.equal(now.hidden, true);
    assert.equal(now.focus, 'cite-a-new', 'Closing from the backdrop returns focus to the citation');

    // Reopening from a real click must work with the capture listener already installed.
    await page.click('#cite-a-new');
    assert.equal((await state()).hidden, false);
    await page.click('#chat-sources-panel .csp-close');

    // Phone width: a strip of backdrop stays visible so the panel can always be dismissed.
    await page.setViewportSize({ width: 375, height: 812 });
    await open('{ label: "S1", trigger: document.getElementById("cite-a-new") }');
    await page.evaluate(() => Promise.all(document.getAnimations().map(animation => animation.finished.catch(() => {}))));
    const phone = await page.evaluate(() => {
      const backdrop = document.getElementById('chat-sources-backdrop').getBoundingClientRect();
      const panel = document.getElementById('chat-sources-panel').getBoundingClientRect();
      return { backdropLeft: backdrop.left, backdropWidth: backdrop.width, panelLeft: panel.left, panelWidth: panel.width };
    });
    assert.ok(phone.panelLeft >= 32 - 1, `Phone panel leaves a strip on the left (left=${phone.panelLeft})`);
    assert.ok(phone.panelWidth <= phone.backdropWidth - 31, 'Phone panel is narrower than the layout');
    await clickAt(phone.panelLeft / 2, 400);
    assert.equal((await state()).hidden, true, 'The strip on a phone dismisses the panel');

    // Back to a wide window: an open panel stops reacting to outside clicks.
    await page.setViewportSize({ width: 1280, height: 800 });
    await open('{ label: "S1", trigger: document.getElementById("cite-a-new") }');
    await page.click('#cite-a-new');
    assert.equal((await state()).hidden, false);
    assert.equal(await backdropDisplay(), 'none');

    assert.deepEqual(errors, []);
    console.log('chat sources panel behavior ok');
  } finally {
    await browser?.close();
    server.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
