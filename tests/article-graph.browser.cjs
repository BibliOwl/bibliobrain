// Run with Node and Playwright already available in NODE_PATH. No live backend is used.
// Optional argument: a folder where screenshots of the screen are written.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const screenshotDir = process.argv[2] || '';

const LIB_PATHS = ['papers/martin.pdf', 'papers/okafor.pdf', 'papers/lee.pdf'];
const LIB_ROWS = [
  ['Attention in cortical circuits', 'Martin', '2021'],
  ['Predictive coding revisited', 'Okafor', '2019'],
  ['Sparse transformers for neural data', 'Lee', '2023'],
];
const GRAPH = {
  ok: true,
  nodes: [
    { id: 'W1', openalex_id: 'W1', title: LIB_ROWS[0][0], year: 2021, authors: ['Martin, A.'], in_notebook: true, source_path: LIB_PATHS[0] },
    { id: 'W2', openalex_id: 'W2', title: LIB_ROWS[1][0], year: 2019, authors: ['Okafor, B.'], in_notebook: true, source_path: LIB_PATHS[1] },
  ],
  edges: [{ source: 'W1', target: 'W2' }],
  similarity_edges: [],
  prior_works: [{ id: 'W9', openalex_id: 'W9', title: 'An older classic', year: 2001, authors: ['Smith, C.'], cited_by_notebook_ids: ['W1', 'W2'] }],
  derivative_works: [{ id: 'W8', openalex_id: 'W8', title: 'A recent follow-up', year: 2024, authors: ['Ng, D.'], cites_notebook_ids: ['W1'] }],
  missing_links: [],
  skipped_sources: [],
  stats: { papers_in_notebook: 2 },
};

async function main() {
  const server = http.createServer((req, res) => {
    const pathname = new URL(req.url, 'http://localhost').pathname;
    if (pathname === '/') {
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end(fs.readFileSync(path.join(root, 'static/index.html'), 'utf8'));
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
    const executablePath = process.env.BIBLIOBRAIN_CHROMIUM || undefined;
    browser = await chromium.launch(executablePath ? { executablePath, headless: true } : { channel: 'msedge', headless: true });
    const page = await browser.newPage({ viewport: { width: 1280, height: 820 }, locale: 'fr-FR' });
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const graphRequests = [];
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/api/library/citation-graph' && route.request().method() === 'POST') {
        graphRequests.push(JSON.parse(route.request().postData() || '{}'));
        await route.fulfill({ status: 200, json: GRAPH });
        return;
      }
      if (url.pathname === '/api/library') {
        await route.fulfill({ status: 200, json: { rows: LIB_ROWS, paths: LIB_PATHS, metadata: LIB_PATHS.map(() => ({})), pagination: { has_more: false } } });
        return;
      }
      if (url.pathname === '/api/notebooks') {
        await route.fulfill({ status: 200, json: { notebooks: [{ id: 'nb1', title: 'Thèse ch. 2' }], pagination: { has_more: false } } });
        return;
      }
      if (url.pathname === '/api/notebooks/nb1') {
        await route.fulfill({ status: 200, json: { id: 'nb1', sources: [{ source_path: LIB_PATHS[2] }, { source_path: '' }] } });
        return;
      }
      await route.fulfill({ status: 200, json: { ok: true } });
    });

    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => typeof setCartographyView === 'function' && typeof showArticleGraphView === 'function');
    await page.evaluate(() => loadLibrary());
    await page.waitForFunction(() => state.libPaths?.length === 3);
    await page.evaluate(({ paths }) => {
      document.getElementById('startup-boot-splash')?.remove();
      document.getElementById('startup-help-modal')?.remove();
      document.getElementById('startup-help-overlay')?.remove();
      state.libSelectedPaths = new Set([paths[0], paths[1]]);
      state.lastRagRetrieval = { query: 'q', sources: [{ file: paths[1] }, { file: 'elsewhere.pdf' }] };
      switchTab('connections');
    }, { paths: LIB_PATHS });

    // The screen opens on the article picker; "Qui cite qui" is gone.
    await page.locator('#article-graph-workspace').waitFor({ state: 'visible' });
    assert.equal(await page.locator('[data-cartography-view="citations"]').count(), 0);
    assert.equal(await page.locator('[data-cartography-view="articles"]').getAttribute('aria-selected'), 'true');
    assert.equal(await page.locator('#knowledge-graph-section').isVisible(), false);
    assert.equal(await page.locator('#article-graph-build').isDisabled(), true);
    assert.equal((await page.locator('#article-graph-from-library .article-graph-shortcut-count').textContent()).trim(), '2');
    assert.equal((await page.locator('#article-graph-from-answer .article-graph-shortcut-count').textContent()).trim(), '1');

    // Search adds a paper; the answer shortcut adds the one library source.
    await page.fill('#article-graph-search', 'martin');
    await page.locator('#article-graph-results [data-article-graph-path]').first().click();
    await page.click('#article-graph-from-answer');
    await page.selectOption('#article-graph-notebook', 'nb1');
    await page.waitForFunction(() => document.querySelectorAll('#article-graph-selected li').length === 3);
    assert.match(await page.locator('#article-graph-count').textContent(), /3 articles/);
    if (screenshotDir) await page.screenshot({ path: path.join(screenshotDir, 'article-graph-picker.png') });

    await page.click('#article-graph-build');
    await page.locator('#nb-citation-graph-modal.is-embedded').waitFor({ state: 'visible' });
    assert.deepEqual(graphRequests.at(-1), { paths: [LIB_PATHS[0], LIB_PATHS[1], LIB_PATHS[2]] });
    assert.equal(await page.locator('#article-graph-builder').isVisible(), false);
    assert.equal(await page.locator('#nb-citation-graph-overlay.open').count(), 0);
    await page.waitForFunction(() => document.querySelectorAll('#nb-cg-stage [data-id], #nb-cg-stage .nb-cg-node').length > 0);
    if (screenshotDir) await page.screenshot({ path: path.join(screenshotDir, 'article-graph-built.png') });

    // The wheel zooms by spreading the graph out: the canvas grows, the dots
    // keep their size, and 100 % brings the stage back to its own size.
    const stageBox = await page.locator('#nb-cg-stage').boundingBox();
    const sizeAt = () => page.evaluate(() => ({ w: _nbCg.layout.W, zoom: _nbCg.zoom, r: _nbCg.layout.targets.get('W1').r, stage: _nbCg.stage.w }));
    const before = await sizeAt();
    await page.mouse.move(stageBox.x + stageBox.width / 2, stageBox.y + stageBox.height / 2);
    for (let i = 0; i < 3; i += 1) await page.mouse.wheel(0, -100);
    await page.waitForFunction(() => _nbCg.zoom > 1.5 && _nbCg.layout.W > _nbCg.stage.w * 1.5);
    const zoomed = await sizeAt();
    assert.equal(zoomed.r, before.r);
    assert.equal(await page.locator('#nb-cg-zoom-reset').isDisabled(), false);
    assert.match(await page.locator('#nb-cg-zoom-reset').textContent(), /^\d+ %$/);
    await page.click('#nb-cg-zoom-reset');
    await page.waitForFunction(() => _nbCg.zoom === 1 && _nbCg.layout.W <= Math.max(_nbCg.stage.w, 520));
    assert.equal(await page.locator('#nb-cg-zoom-reset').textContent(), '100 %');
    // Enter in the search box selects the match.
    await page.fill('#nb-cg-search', 'older classic');
    await page.press('#nb-cg-search', 'Enter');
    await page.waitForFunction(() => _nbCg.selectedNodeId === 'W9');
    assert.equal(await page.locator('#nb-cg-search-count').textContent(), '1 / 1');
    await page.fill('#nb-cg-search', '');

    // The overview keeps the library-wide map; coming back keeps the graph.
    await page.click('[data-cartography-view="library"]');
    assert.equal(await page.locator('#article-graph-workspace').isVisible(), false);
    await page.click('[data-cartography-view="articles"]');
    assert.equal(await page.locator('#nb-citation-graph-modal.is-embedded').isVisible(), true);

    // Back to the picker keeps the chosen papers.
    await page.click('#nb-cg-close');
    await page.locator('#article-graph-builder').waitFor({ state: 'visible' });
    assert.equal(await page.locator('#article-graph-selected li').count(), 3);

    // Library selection bar › "Graphe de citations" builds straight away.
    await page.evaluate(() => switchTab('library'));
    await page.evaluate(() => openArticleGraphFromLibrarySelection());
    await page.locator('#nb-citation-graph-modal.is-embedded').waitFor({ state: 'visible' });
    assert.deepEqual(graphRequests.at(-1), { paths: [LIB_PATHS[0], LIB_PATHS[1]] });

    // The paper sheet still opens the graph as a dialog, and hands the
    // article screen its picker back.
    await page.evaluate(({ path: p }) => _openCitationGraphFrom(`/api/library/citation-graph?path=${encodeURIComponent(p)}`, { scope: { kind: 'article', title: 'x' } }), { path: LIB_PATHS[0] });
    assert.equal(await page.locator('#nb-citation-graph-modal').getAttribute('role'), 'dialog');
    await page.evaluate(() => closeNbCitationGraph());
    assert.equal(await page.locator('#article-graph-citation-workspace').isHidden(), true);
    assert.equal(await page.locator('#article-graph-builder').isHidden(), false);

    assert.deepEqual(errors, []);
    console.log('article-graph browser test: ok');
  } finally {
    await browser?.close();
    server.close();
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
