// Run with Node and Playwright already available in NODE_PATH. No live backend is used.
// While an answer or a literature review runs, the chat send button turns into
// Stop. Screen readers must hear "Arrêter la génération", not "Envoyer le
// message": the i18n observer rewrites title and aria-label from data-i18n-*.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');

const chatSource = fs.readFileSync(path.join(root, 'static/js/app/chat.js'), 'utf8');
function pick(name) {
  const start = chatSource.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} not found in chat.js`);
  return chatSource.slice(start, chatSource.indexOf('\n}\n', start) + 3);
}
const helpers = ['_chatSendIconHtml', '_chatStopIconHtml', '_setSendButtonIdle', '_setSendButtonStop']
  .map(pick)
  .join('\n');

async function launch() {
  try {
    return await chromium.launch({ channel: 'msedge', headless: true });
  } catch {
    return chromium.launch();
  }
}

async function main() {
  const button = fs.readFileSync(path.join(root, 'static/index.html'), 'utf8').match(/<button id="send-btn"[^>]*>/)[0];
  const browser = await launch();
  try {
    const page = await browser.newPage({ locale: 'fr-FR' });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://bibliobrain.test/', route => route.fulfill({
      contentType: 'text/html; charset=utf-8',
      body: `<!doctype html><html><body>${button}</button></body></html>`,
    }));
    await page.route('http://bibliobrain.test/api/**', route => route.fulfill({ status: 404, body: '' }));
    await page.goto('http://bibliobrain.test/');
    await page.addScriptTag({ path: path.join(root, 'static/js/i18n.js') });
    await page.addScriptTag({ content: `function _tr(m) { return window.BibliobrainI18n.translateRaw(m); }\n${helpers}` });

    const expected = {
      fr: { stop: ['Arrêter la génération', 'Arrêter'], idle: ['Envoyer le message', 'Envoyer'] },
      en: { stop: ['Stop generation', 'Stop'], idle: ['Send message', 'Send'] },
    };
    for (const [language, labels] of Object.entries(expected)) {
      await page.evaluate(lang => window.setLanguage(lang), language);
      for (const [state, helper] of [['stop', '_setSendButtonStop'], ['idle', '_setSendButtonIdle']]) {
        await page.evaluate(name => window[name](), helper);
        // Let the i18n MutationObserver run its pass over the changed button.
        await page.waitForTimeout(250);
        const actual = await page.evaluate(() => {
          const btn = document.getElementById('send-btn');
          return [btn.getAttribute('aria-label'), btn.getAttribute('title')];
        });
        assert.deepEqual(actual, labels[state], `${language} ${state}`);
      }
    }
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
  console.log('send-button-label: ok');
}

main().catch(error => {
  console.error(error);
  process.exit(1);
});
