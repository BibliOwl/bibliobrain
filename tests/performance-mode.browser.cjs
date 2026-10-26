// Uses the installed Playwright/Edge, without a model service or user documents.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');
const { createPreviewServer } = require('./performance-mode-server.cjs');

async function main() {
  const server = createPreviewServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({ channel: 'msedge', headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, locale: 'fr-FR', reducedMotion: 'no-preference' });
    const errors = [];
    page.on('pageerror', error => { errors.push(error.message); console.error(error.message); });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => document.body.dataset.performanceFixtureReady === 'true', null, { timeout: 15000 });
    await page.addScriptTag({ url: '/static/vendor/marked/marked.umd.js' });
    await page.addScriptTag({ url: '/static/vendor/dompurify/purify.min.js' });

    const visuals = await page.evaluate(() => {
      const probe = document.createElement('section');
      probe.id = 'performance-probe';
      probe.innerHTML = '<button class="kg-card">Carte</button><span class="spinner"></span><span class="status-wait-text">Recherche en cours…</span><span class="cursor"></span><svg><path class="kg-link active" style="--kg-link-delay:800ms" d="M0 10 L100 10"/><circle class="kg-link-dot" r="4"/></svg><dialog class="small-model-dialog">Chargement en cours…</dialog>';
      document.body.appendChild(probe);
      const dialog = probe.querySelector('dialog');
      dialog.showModal();
      const result = {
        animations: document.getAnimations().length,
        spinner: getComputedStyle(probe.querySelector('.spinner')).display,
        text: getComputedStyle(probe.querySelector('.status-wait-text')).webkitTextFillColor,
        linkOffset: getComputedStyle(probe.querySelector('.kg-link')).strokeDashoffset,
        delay: getComputedStyle(probe.querySelector('.kg-link')).animationDelay,
        backdrop: getComputedStyle(dialog, '::backdrop').backdropFilter,
        filter: getComputedStyle(probe.querySelector('.kg-link')).filter,
        willChange: getComputedStyle(probe.querySelector('.kg-card')).willChange,
        scroll: preferredScrollBehavior(),
      };
      dialog.close();
      return result;
    });
    assert.equal(visuals.animations, 0);
    assert.equal(visuals.spinner, 'none');
    assert.notEqual(visuals.text, 'rgba(0, 0, 0, 0)');
    assert.equal(visuals.linkOffset, '0px');
    assert.equal(visuals.delay, '0s');
    assert.equal(visuals.backdrop, 'none');
    assert.equal(visuals.filter, 'none');
    assert.equal(visuals.willChange, 'auto');
    assert.equal(visuals.scroll, 'instant');
    await page.locator('#performance-probe .kg-card').hover();
    assert.equal(await page.locator('#performance-probe .kg-card').evaluate(el => getComputedStyle(el).transform), 'none');
    await page.locator('#performance-probe .kg-card').focus();
    assert.equal(await page.locator('#performance-probe .kg-card').evaluate(el => getComputedStyle(el).outlineStyle), 'solid');

    await page.evaluate(() => setPerformanceMode('normal'));
    assert.equal(await page.evaluate(() => preferredScrollBehavior()), 'smooth');
    assert.notEqual(await page.locator('#performance-probe .spinner').evaluate(el => getComputedStyle(el).animationName), 'none');
    await page.emulateMedia({ reducedMotion: 'reduce' });
    assert.equal(await page.evaluate(() => preferredScrollBehavior()), 'instant');
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await page.evaluate(() => {
      _nbCg.layout = { targets: new Map([['paper', { x: 42, y: 75 }]]) };
      _nbCg.rafId = requestAnimationFrame(() => {});
      setPerformanceMode('light');
      if (_nbCg.rafId !== null || _nbCg.pos.get('paper').x !== 42) throw new Error('Profile change did not stop graph motion');
      _nbCgStartLoop();
      if (_nbCg.rafId !== null) throw new Error('Light graph loop restarted');
      _nbCg.layout = null;
      document.getElementById('performance-probe').remove();
    });

    const stream = await page.evaluate(() => {
      const root = document.createElement('div');
      root.className = 'bubble bot';
      document.getElementById('messages').appendChild(root);
      const options = { streaming: true, sources: [performanceDemo.source] };
      renderRichAssistantOutput(root, 'Premier paragraphe **terminé**.\n\nDeuxième', options);
      const first = root.querySelector('.assistant-answer p');
      renderRichAssistantOutput(root, 'Premier paragraphe **terminé**.\n\nDeuxième paragraphe [S1].', options);
      const retained = first === root.querySelector('.assistant-answer p');
      const text = '<think>Analyse confidentielle</think>\n\nPremier paragraphe **terminé**.\n\nDeuxième paragraphe [S1].';
      renderRichAssistantOutput(root, text, options);
      const closedThinkingNodes = root.querySelector('.thinking-panel').childNodes.length;
      renderRichAssistantOutput(root, 'Remplacement intégral de la réponse.', options);
      const replaced = root.querySelector('.assistant-answer').textContent.trim() === 'Remplacement intégral de la réponse.';
      const finalOptions = { sources: [performanceDemo.source], normalizeCitations: true };
      renderRichAssistantOutput(root, text, finalOptions);
      const finalHtml = root.innerHTML;
      setPerformanceMode('normal');
      renderRichAssistantOutput(root, text, finalOptions);
      const finalIdentical = root.innerHTML === finalHtml;
      setPerformanceMode('light');
      root.style.display = 'none';
      const previous = root.innerHTML;
      renderRichAssistantOutput(root, 'Réponse reçue en arrière-plan.', options);
      const hiddenUnchanged = root.innerHTML === previous;
      root.style.display = '';
      window.dispatchEvent(new CustomEvent('bibliobrain:tabchange'));
      const resumed = root.textContent.includes('Réponse reçue en arrière-plan.');
      root.style.display = 'none';
      renderRichAssistantOutput(root, 'Texte périmé.', options);
      root.innerHTML = '<p>Erreur de connexion</p>';
      root.style.display = '';
      window.dispatchEvent(new CustomEvent('bibliobrain:tabchange'));
      const errorPreserved = root.textContent === 'Erreur de connexion';
      renderRichAssistantOutput(root, '<img src=x onerror="window.__injected=true">', options);
      const safe = !root.querySelector('[onerror]') && !window.__injected;
      root.remove();
      return { retained, closedThinkingNodes, replaced, finalIdentical, hiddenUnchanged, resumed, errorPreserved, safe, pending: _pendingLightAssistantStreams.size };
    });
    assert.deepEqual(stream, { retained: true, closedThinkingNodes: 0, replaced: true, finalIdentical: true, hiddenUnchanged: true, resumed: true, errorPreserved: true, safe: true, pending: 0 });

    const mutations = await page.evaluate(async () => {
      const root = document.createElement('div');
      root.className = 'bubble bot';
      document.getElementById('messages').appendChild(root);
      const prefix = Array.from({ length: 60 }, (_, i) => `Paragraphe ${i} **stable**.`).join('\n\n');
      const results = {};
      for (const mode of ['normal', 'light']) {
        setPerformanceMode(mode);
        root.replaceChildren();
        renderRichAssistantOutput(root, prefix + '\n\nDébut', { streaming: true, renderMath: false });
        let removed = 0;
        const observer = new MutationObserver(records => {
          removed += records.reduce((sum, record) => sum + [...record.removedNodes].reduce((count, node) => count + 1 + (node.querySelectorAll?.('*').length || 0), 0), 0);
        });
        observer.observe(root, { childList: true, subtree: true });
        const started = performance.now();
        for (let i = 0; i < 30; i++) renderRichAssistantOutput(root, prefix + '\n\nDébut ' + 'du texte '.repeat(i + 1), { streaming: true, renderMath: false });
        const elapsed = performance.now() - started;
        await new Promise(resolve => queueMicrotask(resolve));
        observer.disconnect();
        results[mode] = { removed, elapsedMs: Math.round(elapsed) };
      }
      root.remove();
      return results;
    });
    assert.ok(mutations.light.removed < mutations.normal.removed, JSON.stringify(mutations));

    const output = path.resolve(__dirname, '../output/performance-mode');
    fs.mkdirSync(output, { recursive: true });
    await page.screenshot({ path: path.join(output, 'chat-light.png'), fullPage: true });
    await page.click('.nav-item[data-tab="discovery"]');
    await page.evaluate(() => {
      startDiscoveryProgressMessages('articles');
      window.progressNode = document.querySelector('#discovery-results .empty-state-text');
    });
    await page.waitForTimeout(1150);
    assert.equal(await page.evaluate(() => window.progressNode === document.querySelector('#discovery-results .empty-state-text')), true);
    await page.click('.nav-item[data-tab="chat"]');
    const hiddenText = await page.evaluate(() => window.progressNode.textContent);
    await page.waitForTimeout(1150);
    assert.equal(await page.evaluate(() => window.progressNode.textContent), hiddenText);
    await page.click('.nav-item[data-tab="discovery"]');
    assert.notEqual(await page.evaluate(() => window.progressNode.textContent), hiddenText);
    assert.equal(await page.evaluate(() => document.getAnimations().length), 0);
    await page.screenshot({ path: path.join(output, 'discovery-light.png'), fullPage: true });
    await page.evaluate(() => stopDiscoveryProgressMessages());
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'checks.json'), JSON.stringify({ visuals, stream, mutations }, null, 2));
    console.log('PASS: static visuals, focus, scroll policy, profile changes, graph loop, streaming, final rendering, hidden panels, safe Markdown, progression.');
    console.log(JSON.stringify({ mutations, screenshots: output }));
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
